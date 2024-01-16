"""
This project simplifies RTC Communication through sockets.
"""
import asyncio
import datetime
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, WebSocketException, status, HTTPException
import uvicorn
import secrets
import string
import threading
import time

app = FastAPI()

data = {"gc": {}, "dm": {}}


def cleanup_thread():
    print("Cleanup thread started")
    while True:
        try:
            time.sleep(300)
            for channel in data["gc"]:
                if datetime.datetime.now() - data["gc"][channel]["lastactive"] > datetime.timedelta(hours=1):
                    del data["gc"][channel]
            for channel in data["dm"]:
                if datetime.datetime.now() - data["dm"][channel]["lastactive"] > datetime.timedelta(hours=1):
                    del data["dm"][channel]
        except Exception as e:
            print("ERROR (A01):", e)


async def generate_unique_string(length):
    characters = string.ascii_letters + string.digits
    unique_string = ''.join(secrets.choice(characters) for _ in range(length))
    return unique_string


async def new_chat_token(chat_type):
    token = await generate_unique_string(8)
    while token in data[chat_type]:
        token = await generate_unique_string(8)
    return token


async def new_user_token(chat_type, chat_token):
    token = await generate_unique_string(16)
    while token in data[chat_type][chat_token]["users"]:
        token = await generate_unique_string(16)
    return token


async def validate_token(token, ip, chat_type, chat_token):
    try:
        return ip == data[chat_type][chat_token]["users"][token]["ip"]
    except KeyError:
        return False


async def check_name_exists(chat_type, chat_token, name):
    for user in data[chat_type][chat_token]["users"]:
        if data[chat_type][chat_token]["users"][user]["name"].lower() == name.lower() and "socket" in data[chat_type][chat_token]["users"][user]:
            return True
    return False


async def chat_available(chat_type, chat_token):
    return chat_token in data[chat_type]


async def is_admin(chat_type, chat_token, token, ip):
    valid = await validate_token(chat_token=chat_token, chat_type=chat_type, token=token, ip=ip)
    if not valid:
        return False


async def send_message(chat_type, chat_token, token, data_to_send):
    data[chat_type][chat_token]["lastactive"] = datetime.datetime.now()
    for user in data[chat_type][chat_token]["users"]:
        if user != token:
            if "socket" in data[chat_type][chat_token]["users"][user]:
                await data[chat_type][chat_token]["users"][user]["socket"].send_text(data_to_send)


@app.websocket("/gc")
async def socket_handler(token: str, chat_token: str, websocket: WebSocket):
    """
    This group takes input of socket messages and brodcasts it to other users who are connected to the same group which is identified by the gp token.
    Before sending the message, it will validate if the provided token is valid and the token owner is not blocked/removed from the gc.
    :param token: Token of user which will be used to validate the sender.
    :param chat_token: Token of the chat to which the message needs to be sent.
    :param websocket:
    :return:
    """
    await websocket.accept()
    if not await chat_available("gc", chat_token):
        await websocket.close(1008, "Channel not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    async with data["gc"][chat_token]["lock"]:
        if not await validate_token(token=token, chat_type="gc", chat_token=chat_token, ip=websocket.client.host):
            await websocket.close(1008, "You are not authorized to message in this channel")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="You are not authorized to message in this channel")
        if "socket" in data["gc"][chat_token]["users"][token]:
            await websocket.close(1008, "You are already connected")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are already connected")
        data["gc"][chat_token]["users"][token]["socket"] = websocket
    try:
        while True:
            data_to_send = await websocket.receive_text()
            await send_message(chat_type="gc", chat_token=chat_token, token=token, data_to_send=data_to_send)
    except (WebSocketDisconnect, WebSocketException):
        async with data["gc"][chat_token]["lock"]:
            del data["gc"][chat_token]["users"][token]


@app.websocket("/dm")
async def socket_handler(token: str, chat_token: str, websocket: WebSocket):
    """
    This chat system is one to one chat which is initialized.
    :param token: Token of user which will be used to validate the sender.
    :param chat_token: Token of the chat to which the message needs to be sent.
    :param websocket:
    :return:
    """
    await websocket.accept()
    if not await chat_available("dm", chat_token):
        await websocket.close(1008, "Channel not found")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Channel not found")
    async with data["dm"][chat_token]["lock"]:
        if not await validate_token(token=token, chat_type="dm", chat_token=chat_token, ip=websocket.client.host):
            await websocket.close(1008, "You are not authorized to message in this channel")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="You are not authorized to message in this channel")
        if "socket" in data["dm"][chat_token]["users"][token]:
            await websocket.close(1008, "You are already connected")
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You are already connected")
        data["dm"][chat_token]["users"][token]["socket"] = websocket
    try:
        while True:
            data_to_send = await websocket.receive_text()
            await send_message(chat_type="dm", chat_token=chat_token, token=token, data_to_send=data_to_send)
    except (WebSocketDisconnect, WebSocketException):
        async with data["dm"][chat_token]["lock"]:
            del data["dm"][chat_token]["users"][token]


@app.get("/authtoken")
async def create_token(chat_type: str, chat_token: str, name: str, request: Request):
    """
    Generates a token for user which will be used to validate the sender, and the token will be bound to a spefic chat.
    :param chat_type:
    :param chat_token:
    :param name:
    :param request:
    :return:
    """
    if not await chat_available(chat_type, chat_token):
        return {"alert": "this chat has been deleted"}
    async with data[chat_type][chat_token]["lock"]:
        if await check_name_exists(chat_type, chat_token, name):
            return {"alert": "use other name"}
        if data[chat_type][chat_token]["max_users"] != 0:
            if len(list(data[chat_type][chat_token]["users"].keys())) >= data[chat_type][chat_token]["max_users"]:
                return {"alert": "maximum users reached"}
        token = False
        for user in data[chat_type][chat_token]["admin"]:
            if user[1] == request.client.host and user[0] not in data[chat_type][chat_token]['users']:
                token = user[0]
        if not token:
            token = await new_user_token(chat_type, chat_token)
        data[chat_type][chat_token]["users"][token] = {
            "ip": request.client.host,
            "name": name,
        }
        return token


@app.get("/createchat")
async def create_group(chat_type: str, auto_join: str, max_users: int, allow_dm_betwen_members: str, name: str,
                       request: Request):
    if chat_type == "dm":
        max_users = 2
    if (chat_type not in ["gc", "dm"]) or (auto_join not in ["true", "false"]) or (
            allow_dm_betwen_members not in ["true", "false"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not authorized to create a chat of this type"
        )
    chat_token = await new_chat_token(chat_type)
    superpassword = await generate_unique_string(10)
    data[chat_type][chat_token] = {
        "auto_join": auto_join == "true",
        "max_users": max_users,
        "allow_dm_betwen_members": allow_dm_betwen_members == "true",
        "admin": [],  # [token, ip]
        "users": {},
        "lock": asyncio.Lock(),
        "lastactive": datetime.datetime.now(),
        "superpassword": superpassword,
    }
    user_token = await create_token(chat_type=chat_type, chat_token=chat_token, name=name, request=request)
    return {"token": user_token, "chat_token": chat_token, "chat_type": chat_type, "superpassword": superpassword}

threading.Thread(target=cleanup_thread).start()
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5035)

# http://127.0.0.1:5035/createchat?chat_type=gc&auto_join=true&max_users=0&allow_dm_betwen_members=false&name=test
