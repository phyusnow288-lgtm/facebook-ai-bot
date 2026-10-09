"""Read-only Telegram -> review inbox. Never sends, deletes or approves orders."""
import asyncio
import json
import re
import sqlite3
import urllib.request
from pathlib import Path
from telethon import TelegramClient

ROOT = Path(__file__).resolve().parent
DIGITS = str.maketrans('၀၁၂၃၄၅၆၇၈၉', '0123456789')

def is_order(text):
    text = (text or '').translate(DIGITS)
    return bool(re.search(r'09[\d\s-]{6,}', text) and re.search(r'(?:code\s*[:：-]?\s*\d{1,4}|\b0\d{3}\b)', text, re.I))

def read_api_config(path):
    result = {}
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line:
            k, v = line.split('=', 1)
            result[k.strip()] = v.strip()
    return int(result['API_ID']), result['API_HASH']

def rpc(config, name, payload):
    # Only connector-scoped opaque keys; never use a Supabase service-role key.
    request = urllib.request.Request(config['supabase_url'].rstrip('/') + '/rest/v1/rpc/' + name,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        headers={'Content-Type': 'application/json', 'apikey': config['publishable_key']}, method='POST')
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read()
        if not body.strip():
            if name == 'sync_telegram_messages':
                return None  # PostgreSQL void RPC acknowledges with an empty body.
            raise RuntimeError(name + ': empty response; expected JSON')
        try:
            result = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise RuntimeError(name + ': server response is not JSON') from None
        if name == 'telegram_connector_work' and not isinstance(result, list):
            raise RuntimeError(name + ': expected a JSON list')
        return result

async def call(config, name, **payload):
    return await asyncio.to_thread(rpc, config, name, payload)

def revision(message):
    return int((message.edit_date or message.date).timestamp())

def snapshot(message_id, message=None, nonce=None):
    return dict(message_id=message_id, text=(message.raw_text or '') if message else '',
        revision=revision(message) if message else 0, deleted=message is None, check_nonce=nonce)

async def sync_batch(config, source, messages):
    if messages:
        await call(config, 'sync_telegram_messages',p_source_id=source['source_id'],p_token=source['token'],p_messages=messages)

async def poll_source(client, config, source, state):
    chat = int(source['chat_id'])
    entity = await client.get_entity(chat)
    row = state.execute('select cursor from cursors where source=?', (source['source_id'],)).fetchone()
    if row is None:
        latest = await client.get_messages(entity, limit=1)
        # First run starts with new messages; never silently duplicates already pasted history.
        cursor = int(source.get('start_message_id', latest[0].id if latest else 0))
        state.execute('insert into cursors values(?,?)', (source['source_id'], cursor)); state.commit()
        print('Connected group', chat, '— new messages after', cursor)
    else:
        cursor = row[0]
    uploads = []
    next_cursor = cursor
    async for message in client.iter_messages(entity, min_id=cursor, reverse=True, limit=100):
        if is_order(message.raw_text):
            uploads.append(snapshot(message.id, message))
        next_cursor = message.id
    await sync_batch(config, source, uploads)
    # Advance only after an acknowledged upload. Offline retries keep the same ID.
    state.execute('update cursors set cursor=? where source=?', (next_cursor, source['source_id'])); state.commit()
    work = await call(config, 'telegram_connector_work', p_source_id=source['source_id'], p_token=source['token'])
    for offset in range(0, len(work), 100):
        batch = work[offset:offset+100]
        messages = await client.get_messages(entity, ids=[int(item['message_id']) for item in batch])
        # Telegram RPC failure raises: never interpret a network error as deletion.
        await sync_batch(config, source,[snapshot(int(item['message_id']),message,item.get('check_nonce')) for item,message in zip(batch,messages)])


import os
import signal
from telethon.sessions import StringSession

def cloud_sources():
    configs = json.loads(os.environ['TELEGRAM_CONFIGS_JSON'])
    if not isinstance(configs, list) or not configs:
        raise RuntimeError('TELEGRAM_CONFIGS_JSON must contain exported configurations.')
    sources = [(config, source) for config in configs for source in config['sources'] if source.get('enabled', True)]
    ids = [s['source_id'] for _, s in sources]
    if not sources or len(ids) != len(set(ids)):
        raise RuntimeError('Missing or duplicate Telegram sources.')
    for config, source in sources:
        if not config['supabase_url'].startswith('https://'):
            raise RuntimeError('Supabase URL must use HTTPS.')
        if 'start_message_id' not in source:
            raise RuntimeError('Migration cursor required for every source.')
        int(source['start_message_id'])
    return sources

async def main():
    sources = cloud_sources()
    state_dir = Path(os.environ.get('CONNECTOR_STATE_DIR', '/var/data'))
    if not state_dir.is_dir():
        raise RuntimeError('Mount a persistent disk at CONNECTOR_STATE_DIR before starting.')
    state = sqlite3.connect(state_dir / 'app_connector_state.db')
    state.execute('create table if not exists cursors(source text primary key,cursor integer not null)')
    client = TelegramClient(StringSession(os.environ['TELEGRAM_SESSION']), int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH'])
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError('Cloud Telegram session is not authorized; run local setup again.')
        await client.get_dialogs()
        print('Cloud Telegram connector connected.', flush=True)
        while not stop.is_set():
            for config, source in sources:
                if stop.is_set():
                    break
                try:
                    await poll_source(client, config, source, state)
                except Exception as error:
                    print('Sync retry:', source['chat_id'], type(error).__name__, flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
    finally:
        await client.disconnect()
        state.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except Exception as error:
        print('Connector stopped:', type(error).__name__, flush=True)
        raise SystemExit(1)

