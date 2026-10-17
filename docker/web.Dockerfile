# AI-EOS 前端（Next.js）生产镜像：构建 + 运行两阶段
FROM node:20-alpine AS builder

WORKDIR /app

# 构建期写入的后端地址（浏览器直连后端时用宿主机地址）
ARG NEXT_PUBLIC_API_URL=http://localhost:8001
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

COPY apps/web/package.json ./
RUN npm install --prefer-offline --no-audit --no-fund

COPY apps/web/ ./
RUN npm run build

FROM node:20-alpine

WORKDIR /app
ENV NODE_ENV=production

COPY --from=builder /app/package.json ./
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/.next ./.next

EXPOSE 8000

CMD ["npx", "next", "start", "-p", "8000"]
