"""Fake Telegram Bot API used to drive the real python-telegram-bot polling loop."""
import asyncio, json, time
from telegram.request import BaseRequest

class FakeTelegram(BaseRequest):
    def __init__(self, updates):
        self.pending = list(updates); self.sent = []; self.t0 = time.monotonic()
    async def initialize(self): pass
    async def shutdown(self): pass
    @property
    def read_timeout(self): return 1.0
    async def do_request(self, url, method, request_data=None, **kw):
        name = url.rsplit("/", 1)[-1]
        params = request_data.parameters if request_data else {}
        if name == "getMe":
            r = {"id": 999, "is_bot": True, "first_name": "TONPO", "username": "tonpo_bot",
                 "can_join_groups": True, "can_read_all_group_messages": False, "supports_inline_queries": False}
        elif name == "getUpdates":
            off = int(params.get("offset") or 0)
            r = [u for u in self.pending if u["update_id"] >= off]
            if not r: await asyncio.sleep(0.2)
        elif name in ("sendMessage", "sendPhoto", "editMessageText"):
            self.sent.append((round(time.monotonic() - self.t0, 2), params.get("chat_id"), str(params.get("text", ""))[:70]))
            r = {"message_id": len(self.sent) + 100, "date": int(time.time()),
                 "chat": {"id": int(params.get("chat_id", 1)), "type": "private"}, "text": params.get("text", "")}
        elif name == "getChatMember":
            r = {"status": "administrator", "user": {"id": 999, "is_bot": True, "first_name": "TONPO"},
                 "can_post_messages": True, "can_be_edited": False, "is_anonymous": False, "can_manage_chat": True,
                 "can_delete_messages": True, "can_manage_video_chats": True, "can_restrict_members": True,
                 "can_promote_members": False, "can_change_info": True, "can_invite_users": True,
                 "can_post_stories": True, "can_edit_stories": True, "can_delete_stories": True}
        elif name == "getChat":
            r = {"id": int(params.get("chat_id", 1)), "type": "supergroup", "title": "Signals"}
        else:
            r = True
        return 200, json.dumps({"ok": True, "result": r}).encode()

def msg_update(uid, text, user_id=111, chat_type="private"):
    ent = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}] if text.startswith("/") else []
    return {"update_id": uid, "message": {"message_id": uid, "date": int(time.time()),
            "chat": {"id": user_id, "type": chat_type}, "from": {"id": user_id, "is_bot": False, "first_name": "Owner"},
            "text": text, "entities": ent}}
