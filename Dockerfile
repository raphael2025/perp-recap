FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY perp_recap ./perp_recap
RUN pip install --no-cache-dir .
COPY app.py ./
COPY examples ./examples
COPY .streamlit ./.streamlit
EXPOSE 8501
CMD ["python", "-m", "streamlit", "run", "app.py", "--server.address=0.0.0.0", "--server.headless=true"]
