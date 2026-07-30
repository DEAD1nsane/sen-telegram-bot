FROM mcr.microsoft.com/playwright:v1.42.0-noble

WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

CMD ["node", "bot.js"]
