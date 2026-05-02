ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base-python:3.13-alpine3.22
FROM $BUILD_FROM
 
# Install pip dependencies
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt
 
# Copy Python source
COPY *.py .
COPY translation.json .
 
# Copy startup script
COPY run.sh /run.sh
RUN chmod +x /run.sh
 
CMD ["/run.sh"]