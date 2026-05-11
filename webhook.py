"""Mai HR Bot — Lark webhook → Dify → reply.

Chạy: python webhook.py (port 5000)
Bypass plugin Lark Trigger của Dify, gọi Dify API trực tiếp.
"""

import os
import json
import time
import base64
import hashlib
import logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('mai')

LARK_APP_ID = os.getenv('LARK_APP_ID', '').strip()
LARK_APP_SECRET = os.getenv('LARK_APP_SECRET', '').strip()
LARK_VERIFICATION_TOKEN = os.getenv('LARK_VERIFICATION_TOKEN', '').strip()
LARK_ENCRYPT_KEY = os.getenv('LARK_ENCRYPT_KEY', '').strip()
DIFY_API_KEY = os.getenv('DIFY_API_KEY', '').strip()
DIFY_BASE = os.getenv('DIFY_BASE', 'https://api.dify.ai/v1').rstrip('/')

LARK_BASE = 'https://open.larksuite.com/open-apis'

app = Flask(__name__)
_token = {'val': None, 'exp': 0}
_dedup = {}
_recent = []  # ring buffer of last 30 webhook hits


def lark_decrypt(encrypted_str: str, key: str) -> dict:
    from Crypto.Cipher import AES
    key_hash = hashlib.sha256(key.encode('utf-8')).digest()
    cipher_b = base64.b64decode(encrypted_str)
    iv, ct = cipher_b[:16], cipher_b[16:]
    pt = AES.new(key_hash, AES.MODE_CBC, iv).decrypt(ct)
    pad = pt[-1]
    return json.loads(pt[:-pad].decode('utf-8'))


def get_tenant_token() -> str:
    if _token['val'] and _token['exp'] > time.time() + 60:
        return _token['val']
    r = requests.post(
        f'{LARK_BASE}/auth/v3/tenant_access_token/internal',
        json={'app_id': LARK_APP_ID, 'app_secret': LARK_APP_SECRET},
        timeout=10,
    )
    d = r.json()
    if d.get('code') != 0:
        raise RuntimeError(f'Lark auth failed: {d}')
    _token['val'] = d['tenant_access_token']
    _token['exp'] = time.time() + d.get('expire', 7200)
    return _token['val']


def call_dify(query: str, user_id: str, conversation_id: str = '') -> str:
    r = requests.post(
        f'{DIFY_BASE}/chat-messages',
        headers={'Authorization': f'Bearer {DIFY_API_KEY}', 'Content-Type': 'application/json'},
        json={
            'inputs': {},
            'query': query,
            'response_mode': 'blocking',
            'user': user_id,
            'conversation_id': conversation_id,
        },
        timeout=60,
    )
    d = r.json()
    if 'answer' not in d:
        log.error('Dify error: %s', d)
        return f'(Dify lỗi) {d.get("message", d)}'
    return d['answer']


def reply_lark(message_id: str, text: str) -> dict:
    token = get_tenant_token()
    r = requests.post(
        f'{LARK_BASE}/im/v1/messages/{message_id}/reply',
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'},
        json={'msg_type': 'text', 'content': json.dumps({'text': text})},
        timeout=10,
    )
    return r.json()


@app.route('/webhook', methods=['POST'])
def webhook():
    raw = request.get_json(silent=True) or {}
    _recent.append({'ts': time.time(), 'body': raw})
    if len(_recent) > 30:
        _recent.pop(0)

    if 'encrypt' in raw:
        if not LARK_ENCRYPT_KEY:
            log.warning('Encrypted payload received but LARK_ENCRYPT_KEY is empty')
            return jsonify({'error': 'encrypt_key_missing'}), 400
        try:
            body = lark_decrypt(raw['encrypt'], LARK_ENCRYPT_KEY)
        except Exception as e:
            log.exception('Decrypt failed: %s', e)
            return jsonify({'error': 'decrypt_failed'}), 400
    else:
        body = raw

    if body.get('type') == 'url_verification':
        log.info('URL verification challenge')
        return jsonify({'challenge': body.get('challenge')})

    header = body.get('header', {}) or {}
    if LARK_VERIFICATION_TOKEN:
        token_in = header.get('token') or body.get('token')
        if token_in and token_in != LARK_VERIFICATION_TOKEN:
            log.warning('Verification token mismatch')
            return jsonify({'error': 'token_mismatch'}), 403

    event_id = header.get('event_id')
    if event_id:
        if event_id in _dedup:
            return jsonify({'ok': True, 'dedup': True})
        _dedup[event_id] = time.time()
        for k in [k for k, v in _dedup.items() if time.time() - v > 600]:
            _dedup.pop(k, None)

    event_type = header.get('event_type') or body.get('event', {}).get('type')
    if event_type == 'im.message.receive_v1':
        event = body.get('event', {})
        msg = event.get('message', {})
        sender = event.get('sender', {})
        message_id = msg.get('message_id')
        try:
            content_obj = json.loads(msg.get('content', '{}'))
        except Exception:
            content_obj = {}
        text = (content_obj.get('text') or '').strip()
        for tag in ['@_user_1', '@_all']:
            text = text.replace(tag, '').strip()
        if not text:
            return jsonify({'ok': True, 'skip': 'empty'})
        user_id = sender.get('sender_id', {}).get('open_id') or sender.get('sender_id', {}).get('user_id') or 'anon'
        log.info('msg from %s: %s', user_id, text[:80])
        try:
            answer = call_dify(text, user_id)
        except Exception as e:
            log.exception('Dify call error')
            answer = f'(Lỗi gọi AI: {e})'
        try:
            reply_lark(message_id, answer)
        except Exception as e:
            log.exception('Lark reply error')

    return jsonify({'ok': True})


@app.route('/', methods=['GET'])
def home():
    return f'Mai HR Bot OK | encrypt={"on" if LARK_ENCRYPT_KEY else "off"} | token={"on" if LARK_VERIFICATION_TOKEN else "off"}'


@app.route('/recent', methods=['GET'])
def recent():
    return jsonify(_recent)


@app.route('/bot-info', methods=['GET'])
def bot_info():
    """Query Lark for bot self-info to verify capabilities."""
    try:
        token = get_tenant_token()
        r = requests.get(
            f'{LARK_BASE}/bot/v3/info',
            headers={'Authorization': f'Bearer {token}'},
            timeout=10,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/app-info', methods=['GET'])
def app_info():
    """Query Lark for app info — version status, scope, etc."""
    try:
        token = get_tenant_token()
        r = requests.get(
            f'{LARK_BASE}/application/v6/applications/{LARK_APP_ID}',
            headers={'Authorization': f'Bearer {token}'},
            params={'lang': 'en_us'},
            timeout=10,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/send-test/<open_id>', methods=['GET'])
def send_test(open_id):
    """Send a test DM directly via Lark API. Verifies bot can SEND DM."""
    try:
        token = get_tenant_token()
        r = requests.post(
            f'{LARK_BASE}/im/v1/messages',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'},
            params={'receive_id_type': 'open_id'},
            json={
                'receive_id': open_id,
                'msg_type': 'text',
                'content': json.dumps({'text': 'Tin nhắn test từ webhook diag — bot CÓ thể gửi DM. Nếu bạn nhận được tin này, vấn đề là ở receive side.'}),
            },
            timeout=10,
        )
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/diag', methods=['GET'])
def diag():
    """Diagnostic endpoint — test Dify + Lark credentials end-to-end."""
    report = {
        'lark_app_id': LARK_APP_ID or '(missing)',
        'lark_app_secret_set': bool(LARK_APP_SECRET),
        'dify_api_key_set': bool(DIFY_API_KEY),
        'dify_base': DIFY_BASE,
    }

    try:
        token = get_tenant_token()
        report['lark_auth'] = 'ok'
        report['lark_token_prefix'] = (token or '')[:10] + '...'
    except Exception as e:
        report['lark_auth'] = f'FAIL: {e}'

    try:
        ans = call_dify('test ping', 'diag_user')
        report['dify_call'] = 'ok'
        report['dify_answer_preview'] = (ans or '')[:200]
    except Exception as e:
        report['dify_call'] = f'FAIL: {e}'

    return jsonify(report)


if __name__ == '__main__':
    missing = [k for k, v in {
        'LARK_APP_ID': LARK_APP_ID,
        'LARK_APP_SECRET': LARK_APP_SECRET,
        'DIFY_API_KEY': DIFY_API_KEY,
    }.items() if not v]
    if missing:
        log.warning('Missing env: %s — server will start but cannot reply.', ', '.join(missing))

    print()
    print('=' * 50)
    print('  MAI HR BOT — webhook server')
    print('=' * 50)
    print(f'  Lark App ID:  {LARK_APP_ID or "(missing)"}')
    print(f'  Dify base:    {DIFY_BASE}')
    print(f'  Encryption:   {"ON" if LARK_ENCRYPT_KEY else "OFF (plain)"}')
    print(f'  Token verify: {"ON" if LARK_VERIFICATION_TOKEN else "OFF"}')
    print(f'  Listening on: http://0.0.0.0:5000/webhook')
    print('=' * 50 + '\n')

    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
