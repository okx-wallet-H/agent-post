import os, sys
sys.path.insert(0, "/Users/h/Desktop/温暖Agent助手/产品/agent-post-repo/server")
import main
import help_page
main.app.include_router(help_page.router)
import uvicorn
uvicorn.run(main.app, host="0.0.0.0", port=main.PORT, log_level="warning")
