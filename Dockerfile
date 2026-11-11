FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py ./
COPY static ./static
ENV DATA_DIR=/data
EXPOSE 5056
CMD ["gunicorn", "-b", "0.0.0.0:5056", "-w", "1", "--threads", "4", "app:app"]
