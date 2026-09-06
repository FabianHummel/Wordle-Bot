import asyncio
import os
import sys
import json
import time
from dotenv import load_dotenv
from nio import (
    AsyncClient,
    AsyncClientConfig,
    MatrixRoom,
    RoomMessageText,
    LoginResponse,
    InviteMemberEvent,
    JoinError,
    LocalProtocolError,
    MegolmEvent,
)

load_dotenv()

HOMESERVER = os.getenv("MATRIX_HOMESERVER")
USERNAME = os.getenv("MATRIX_USER")
PASSWORD = os.getenv("MATRIX_PASSWORD")
DEVICE_NAME = os.getenv("MATRIX_DEVICE_NAME", "matrix-bot")

STORE_PATH = "./store"
DEVICE_ID_FILE = "./device_id.json"
os.makedirs(STORE_PATH, exist_ok=True)


def load_device_id():
    if os.path.exists(DEVICE_ID_FILE):
        with open(DEVICE_ID_FILE) as f:
            return json.load(f).get("device_id")
    return None


def save_device_id(device_id):
    with open(DEVICE_ID_FILE, "w") as f:
        json.dump({"device_id": device_id}, f)


async def message_callback(room: MatrixRoom, event: RoomMessageText):
    if event.sender == client.user_id:
        return
    print(f"[{room.display_name}] {event.sender}: {event.body}")
    if event.body.startswith("!touch"):
        await send(room.room_id, f"Hello {event.sender.split(':')[0]}, it is currently {time.ctime(time.time())}. Have a nice day!")
    elif event.body.startswith("!echo "):
        await send(room.room_id, event.body[len("!echo "):])


async def send(room_id, text):
    try:
        await client.room_send(
            room_id=room_id,
            message_type="m.room.message",
            content={"msgtype": "m.text", "body": text},
            ignore_unverified_devices=True,
        )
    except LocalProtocolError as e:
        print(f"Send failed (encryption issue): {e}")


async def invite_callback(room: MatrixRoom, event: InviteMemberEvent):
    url = f"{client.homeserver}/_matrix/client/v3/join/{room.room_id}"
    headers = {"Authorization": f"Bearer {client.access_token}"}
    async with client.client_session.post(url, data=json.dumps({}), headers=headers) as resp:
        text = await resp.text()
        print(f"Join response: {resp.status} {text}")




async def undecrypted_callback(room, event: MegolmEvent):
    print(f"Could not decrypt {event.event_id} in {room.room_id} — requesting key")
    try:
        await client.request_room_key(event)
    except Exception as e:
        print(f"Key request note: {e}")


async def main():
    global client

    config = AsyncClientConfig(
        store_sync_tokens=True,
        encryption_enabled=True,
    )

    saved_device_id = load_device_id()

    client = AsyncClient(
        HOMESERVER,
        USERNAME,
        store_path=STORE_PATH,
        config=config,
        device_id=saved_device_id,
    )

    resp = await client.login(PASSWORD, device_name=DEVICE_NAME)
    if not isinstance(resp, LoginResponse):
        print(f"Login failed: {resp}")
        sys.exit(1)

    print(f"Logged in as {client.user_id}, device {client.device_id}")
    save_device_id(client.device_id)

    if client.should_upload_keys:
        await client.keys_upload()

    client.add_event_callback(message_callback, RoomMessageText)
    client.add_event_callback(invite_callback, InviteMemberEvent)
    client.add_event_callback(undecrypted_callback, MegolmEvent)

    await client.sync_forever(timeout=50000, full_state=True)


if __name__ == "__main__":
    asyncio.run(main())
