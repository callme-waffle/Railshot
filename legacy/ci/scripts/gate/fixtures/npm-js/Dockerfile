FROM node:22.23.3-bookworm-slim
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install --global npm@12.2.0 --ignore-scripts --no-audit --no-fund && test "$(npm --version)" = 12.2.0 && npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY src ./src
USER 10001
CMD ["node", "src/server.js"]
