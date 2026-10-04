import os, sys
sys.path.insert(0, "/Users/h/Desktop/温暖Agent助手/产品/agent-post-repo/server")
import main
import chat
chat.attach(db=main._db, lock=main._db_lock, user_token=main.USER_TOKEN,
            q=main.q, q1=main.q1, ex=main.ex, new_id=main.new_id,
            now_iso=main.now_iso, new_token=lambda: None)
main.app.include_router(chat.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
