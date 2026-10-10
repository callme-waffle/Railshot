FROM node:22.23.3-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY server.js ./
USER 10001
EXPOSE 8080 9400
CMD ["node", "server.js"]
