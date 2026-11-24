# Local-IA

API local para hablar con un modelo desde la terminal o desde otra aplicación. En
Mac con Apple silicon, arranca **un único proceso MLX cuando llega una consulta**,
lo reutiliza y lo cierra tras **5 minutos sin actividad**. La API permanece disponible.

```text
Terminal / aplicación → API :8001 → MLX :8080 → modelo local
                       siempre      bajo demanda
```

El proyecto puede estar dentro de MAMP, pero **no necesita Apache, PHP ni MySQL**.
Se ejecuta con Python y escucha únicamente en `127.0.0.1`.

## Empezar en esta Mac

```bash
cd /Applications/MAMP/htdocs/Local-IA
bash "mac install/install.sh"
./local-ia start
./local-ia chat "Explícame qué es una API en dos frases"
```

El instalador reutiliza MLX si ya está instalado y conserva su intérprete en `.env`.
Las dependencias de la API se instalan en `.venv`, sin modificar Python global.
El modelo predeterminado es `mlx-community/Qwen1.5-0.5B-Chat-4bit`, pequeño y adecuado
para probar el funcionamiento; su capacidad de respuesta es limitada.

Por defecto se trabaja **sin descargas** (`LOCAL_IA_OFFLINE=1`). Para un modelo que
todavía no tengas, cambia `LOCAL_IA_OFFLINE=0` en `.env` antes de la primera consulta;
MLX descargará sus archivos. Después puedes volver a `1`. El primer uso puede
tardar más por la carga o descarga. El modelo sigue ocupando espacio en disco cuando
el proceso se cierra.

Si tenías el puente anterior con `uvicorn --reload` y un `mlx_lm.server` manual,
detén ambos con Ctrl+C en sus terminales antes de usar `start`. El gestor no toma
control ni mata servidores ajenos que ya ocupen el puerto.

## Terminal

```bash
./local-ia start                # API en segundo plano, modelo inicialmente apagado
./local-ia chat                 # Chat interactivo: /nuevo o /salir
./local-ia chat "Hola" --max-tokens 100
./local-ia status               # Estado y PID, sin cargar el modelo
./local-ia unload               # Liberar memoria ahora; conserva la API
./local-ia stop                 # Detener la API iniciada con start
./local-ia serve                # Alternativa: primer plano, Ctrl+C para terminar
```

No se instala ningún servicio de arranque automático. Después de reiniciar la Mac,
usa `./local-ia start`. Los registros están en `.local-ia/api.log` y
`.local-ia/backend.log`. No uses `--reload` ni varios workers en el uso habitual.

## Conectarse desde curl

```bash
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8001/v1/models

curl -N http://127.0.0.1:8001/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"default_model","messages":[{"role":"user","content":"Hola, responde en español"}],"max_tokens":128,"stream":true}'

curl -X POST http://127.0.0.1:8001/v1/unload
```

Sin `stream` o con `stream:false`, devuelve JSON completo. Con `stream:true`,
devuelve eventos SSE conforme se generan, terminando con `data: [DONE]`.
Acepta `temperature`, `top_p`, `stop`, `max_tokens` o `max_completion_tokens`, y
`stream_options.include_usage`. El cliente envía el historial en `messages`.
Es un **subconjunto de chat de texto** del formato OpenAI: no implementa herramientas,
imágenes, audio, embeddings, Responses ni el resto de sus endpoints.

| Ruta | Función |
| --- | --- |
| `GET /health` | Comprueba la API; un motor apagado es un estado normal |
| `GET /v1/status` | Estado del motor, solicitudes activas, cola y tiempo hasta apagado |
| `GET /v1/models` | Modelo configurado, sin cargarlo |
| `POST /v1/chat/completions` | Generación de texto, JSON o SSE |
| `POST /v1/unload` | Cierra el motor propio si no hay consultas activas o en cola |
| `GET /docs` | Documentación interactiva de la API |

## Qué reduce el consumo

- No importa MLX ni carga los pesos dentro del proceso permanente de la API.
- Comparte un motor entre las terminales que usen esta API, con una generación a la vez.
- Cola acotada a 8 consultas; las solicitudes que no caben o esperan demasiado reciben 429.
- Cierra su proceso MLX al estar inactivo, al cerrar la API o al cancelar un flujo incompleto.
- Reutiliza conexiones HTTP y transmite la respuesta sin esperar a recibirla completa.
- Limita la caché de conversaciones de MLX a una entrada y 64 MB.
- Limita entrada a 12 000 caracteres y salida a 1 024 tokens (256 por defecto).

Estos son límites de trabajo, **no una garantía de RAM máxima**. Los caracteres no
equivalen a tokens. El modelo y el contexto siguen consumiendo CPU/GPU y memoria
mientras se usan. La caché de 64 MB no incluye todos los pesos ni toda la memoria
temporal. Descargar de memoria añade tiempo a la siguiente consulta. No se aplican
cachés de respuestas que puedan devolver información vieja.

## Configuración

Copia `.env.example` a `.env` si no ejecutaste el instalador. Las variables del
entorno tienen prioridad. Reinicia la API después de cambiar la configuración:

```bash
./local-ia stop
./local-ia start
```

| Variable | Predeterminado | Uso |
| --- | --- | --- |
| `LOCAL_IA_MODEL` | `mlx-community/Qwen1.5-0.5B-Chat-4bit` | Único modelo admitido |
| `LOCAL_IA_MLX_PYTHON` | Python de la API | Intérprete donde está instalado MLX LM 0.32.2+ |
| `LOCAL_IA_IDLE_SECONDS` | `300` | Segundos sin consultas antes de cerrar MLX |
| `LOCAL_IA_PORT` | `8001` | Puerto de la API |
| `LOCAL_IA_BACKEND_PORT` | `8080` | Puerto privado del proceso MLX |
| `LOCAL_IA_DEFAULT_TOKENS` | `256` | Longitud máxima de respuesta por defecto |
| `LOCAL_IA_MAX_TOKENS` | `1024` | Límite de tokens solicitables |
| `LOCAL_IA_MAX_INPUT_CHARS` | `12000` | Límite de caracteres del historial |
| `LOCAL_IA_MAX_QUEUE` | `8` | Consultas en espera |
| `LOCAL_IA_QUEUE_TIMEOUT` | `60` | Espera máxima por turno en segundos |
| `LOCAL_IA_STARTUP_TIMEOUT` | `90` | Espera por el servidor MLX |
| `LOCAL_IA_REQUEST_TIMEOUT` | `180` | Tiempo máximo sin datos del motor |
| `LOCAL_IA_OFFLINE` | `1` | Solo caché local; `0` permite descargas |

Para conectar un motor existente, configura `LOCAL_IA_MODE=external`,
`LOCAL_IA_EXTERNAL_URL=http://127.0.0.1:11434` (por ejemplo, Ollama) y el identificador
real de un modelo instalado en ese motor. En este modo, el servidor externo controla
su propia carga y descarga de memoria; `/v1/unload` devuelve 409 y nunca lo detiene.
El instalador de Windows prepara este modo; la gestión automática de MLX requiere Mac
con Apple silicon. La API está pensada para uso local, no para publicarla en Internet.

## Comprobaciones

Pruebas de API y ciclo de vida sin modelo ni GPU:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Prueba real opcional en Mac, con el modelo ya descargado y puertos 18001/18080 libres:

```bash
LOCAL_IA_MLX_PYTHON=/ruta/al/python/con/mlx .venv/bin/python scripts/smoke_mlx.py
```

Verifica carga inicial, respuesta JSON, SSE, reutilización, concurrencia, cancelación,
descarga manual y apagado por inactividad. Cierra sus procesos al finalizar y guarda
un informe local en `.local-ia/smoke-report.json`. Las cifras RSS del informe son
observaciones del proceso, no mediciones de consumo total del sistema ni de toda la GPU.
