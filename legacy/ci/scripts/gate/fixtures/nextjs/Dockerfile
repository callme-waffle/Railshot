FROM node:22.23.3-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm install --global npm@12.2.0 --ignore-scripts --no-audit --no-fund && test "$(npm --version)" = 12.2.0 && npm ci --ignore-scripts --no-audit --no-fund
COPY . .
RUN npm run build
FROM node:22.23.3-bookworm-slim
WORKDIR /app
ENV PORT=8080 HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
USER 10001
CMD ["node", "server.js"]
