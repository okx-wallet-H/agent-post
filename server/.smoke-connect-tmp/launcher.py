import os, sys
sys.path.insert(0, "/Users/h/Desktop/温暖Agent助手/产品/agent-post-repo/server")
import main
import onboard
main.app.include_router(onboard.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
