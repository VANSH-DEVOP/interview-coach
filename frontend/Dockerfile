# ---------------------------------------------------------------------------
# InterviewPilot AI - Frontend (Next.js, standalone output, multi-stage)
# ---------------------------------------------------------------------------
FROM node:20-alpine AS deps
WORKDIR /srv/frontend
COPY package.json package-lock.json* ./
RUN npm install --frozen-lockfile 2>/dev/null || npm install

FROM node:20-alpine AS builder
WORKDIR /srv/frontend
COPY --from=deps /srv/frontend/node_modules ./node_modules
COPY . .
ARG NEXT_PUBLIC_API_URL=http://localhost:8000/api/v1
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build

FROM node:20-alpine AS runner
WORKDIR /srv/frontend
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1

RUN addgroup --system --gid 1001 nodejs \
    && adduser --system --uid 1001 nextjs

COPY --from=builder --chown=nextjs:nodejs /srv/frontend/.next/standalone ./
COPY --from=builder --chown=nextjs:nodejs /srv/frontend/.next/static ./.next/static
COPY --from=builder --chown=nextjs:nodejs /srv/frontend/public ./public

USER nextjs
EXPOSE 3000
CMD ["node", "server.js"]
