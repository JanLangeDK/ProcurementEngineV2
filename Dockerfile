FROM python:3.12-slim
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv
COPY pyproject.toml ./
COPY src ./src
COPY fixtures ./fixtures
RUN uv pip install --system .
ENV PEV2_DB=/data/pev2.db
RUN mkdir -p /data
EXPOSE 7860 8000
CMD ["pev2", "ui"]
