# Cliente 1 - Alpha Tech (Node.js + Express)
FROM node:22-alpine
ENV NODE_ENV=production PORT=3000
WORKDIR /app
# Primero solo package.json: Docker reutiliza esta capa si el código cambia pero las dependencias no
COPY package.json ./
RUN npm install --omit=dev && npm cache clean --force
COPY src ./src
# Se ejecuta con el usuario sin privilegios "node", no como root
USER node
EXPOSE 3000
CMD ["node", "src/server.js"]
