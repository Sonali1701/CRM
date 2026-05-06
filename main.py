import uvicorn
from dotenv import load_dotenv

load_dotenv()

from app.main import app  # noqa: E402 – load env before app imports

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
