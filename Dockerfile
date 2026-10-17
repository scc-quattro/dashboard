FROM python:3.12-slim
WORKDIR /app
COPY collector.py server.py traces.py ./
COPY static ./static
COPY examples ./examples
USER 65534:65534
EXPOSE 8790
CMD ["python3", "server.py", "--host", "0.0.0.0", "--port", "8790"]
