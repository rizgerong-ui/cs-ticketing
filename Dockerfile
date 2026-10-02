FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py postgres.py validation.py sheets_import.py staff_auth.py user_access.py ./
COPY bootstrap.json ./
COPY index.html login.html validation-ui.js users-ui.js validation.css ./
ENV PYTHONUNBUFFERED=1 TZ=Asia/Taipei
RUN useradd --uid 10001 --create-home app
USER app
CMD ["python", "server.py"]
