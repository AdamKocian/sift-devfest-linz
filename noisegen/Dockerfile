FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY noisegen ./noisegen
RUN pip install --no-cache-dir .
USER nobody
ENTRYPOINT ["noisegen"]
