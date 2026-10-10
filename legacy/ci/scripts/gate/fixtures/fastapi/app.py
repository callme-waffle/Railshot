from fastapi import FastAPI

app = FastAPI()


def message() -> str:
    return "ready"


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": message()}
