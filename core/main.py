# core/main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import httpx
import os
import platform

app = FastAPI(title="LocalAI Bridge")

# URL del backend (por defecto apunta a mlx_lm.server en el puerto 8080)
BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8080")

class ChatRequest(BaseModel):
    model: str = "default_model"
    messages: list
    stream: bool = False

@app.get("/health")
async def health():
    return {"status": "ok", "platform": platform.system()}

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    """
    Reenvía la petición al backend de inferencia local (MLX u Ollama).
    """
    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            # Reenviamos la petición al modelo de IA local
            resp = await client.post(
                f"{BACKEND_URL}/v1/chat/completions",
                json=req.model_dump(),
                headers={"Content-Type": "application/json"}
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.ConnectError:
            raise HTTPException(
                status_code=503, 
                detail="El backend no está iniciado. Ejecuta primero mlx_lm.server u Ollama."
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8001)