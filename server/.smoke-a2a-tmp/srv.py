import os, sys
sys.path.insert(0, "/Users/h/Desktop/温暖Agent助手/产品/agent-post-repo/server")
os.environ["HUB_DB"] = "/Users/h/Desktop/温暖Agent助手/产品/agent-post-repo/server/.smoke-a2a-tmp/a2a.db"
os.environ["HUB_USER_TOKEN"] = "h-dev-token"
import main as hub
import api_v1
import a2a
a2a.attach(db=hub._db, lock=hub._db_lock, user_token=hub.USER_TOKEN,
           q=hub.q, q1=hub.q1, ex=hub.ex, tenancy=api_v1._t())
hub.app.include_router(a2a.router)
import uvicorn
uvicorn.run(hub.app, host="127.0.0.1", port=9264, log_level="error")
