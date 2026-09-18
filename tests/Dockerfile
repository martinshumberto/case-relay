FROM python:3.12-slim
WORKDIR /tests
COPY tests/requirements.txt .
RUN pip install -r requirements.txt
COPY tests/ .
CMD ["pytest", "-q", "--tb=short"]
