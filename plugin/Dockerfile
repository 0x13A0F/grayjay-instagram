# Plugin server image: bakes the plugin source + configure.py in, so it needs
# NO host bind mount (works with git-based deploy tools like dockhand). At
# container start, configure.py renders ./dist from the runtime env
# (IG_API_BASE / IG_API_KEY / IG_SOURCE_URL), which is then http-served on :8080.
FROM python:3.12-slim
WORKDIR /plugin
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . /plugin
EXPOSE 8080
CMD ["sh", "-c", "python configure.py && python -m http.server --directory dist 8080"]
