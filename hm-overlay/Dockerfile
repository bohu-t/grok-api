FROM ghcr.io/hm2899/grokcli-2api:1.9.91

ENV DEBIAN_FRONTEND=noninteractive PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
      xvfb wget gnupg ca-certificates fonts-noto-cjk \
      libasound2 libatk-bridge2.0-0 libatk1.0-0 libc6 libcairo2 libcups2 \
      libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
      libpango-1.0-0 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
      libxext6 libxfixes3 libxrandr2 xdg-utils \
    && wget -qO- https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor >/usr/share/keyrings/google-linux.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" >/etc/apt/sources.list.d/google-chrome.list \
    && apt-get update && apt-get install -y --no-install-recommends google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/local-task-requirements.txt
RUN pip install --no-cache-dir -r /tmp/local-task-requirements.txt
COPY hm-overlay/hm_app.py /app/grok2api/app.py
COPY hm-overlay/local_tasks.py /app/grok2api/admin/local_tasks.py
COPY hm-overlay/admin_routes.py /app/grok2api/admin/admin_routes.py
COPY hm-overlay/static/admin/register-tasks.html /app/static/admin/register-tasks.html
COPY hm-overlay/static/admin-task-bridge.js /app/static/admin-task-bridge.js
COPY hm-overlay/DrissionPage_example.py hm-overlay/email_register.py hm-overlay/config.example.json /opt/local-register-source/
COPY hm-overlay/turnstilePatch /opt/local-register-source/turnstilePatch
COPY hm-overlay/static/core.d60204b9f3.js /app/static/dist/core.d60204b9f3.js
COPY hm-overlay/static/admin /app/static/admin
