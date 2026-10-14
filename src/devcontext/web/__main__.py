import os
import uvicorn


def main():
    uvicorn.run("devcontext.web.api:create_app", factory=True,
                host=os.environ.get("DEVCONTEXT_WEB_HOST", "127.0.0.1"),
                port=int(os.environ.get("DEVCONTEXT_WEB_PORT", "8000")), workers=1)


if __name__ == "__main__":
    main()
