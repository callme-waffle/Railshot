FROM node:22.23.3-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install --global npm@12.2.0 --ignore-scripts --no-audit --no-fund && test "$(npm --version)" = 12.2.0 && npm ci --no-audit --no-fund
COPY . .
RUN npm run build
FROM node:22.23.3-bookworm-slim
WORKDIR /app
COPY --from=build /app/package.json /app/package-lock.json ./
RUN npm install --global npm@12.2.0 --ignore-scripts --no-audit --no-fund && test "$(npm --version)" = 12.2.0 && npm ci --omit=dev --ignore-scripts --no-audit --no-fund
COPY --from=build /app/dist ./dist
USER 10001
CMD ["node", "dist/server.js"]
