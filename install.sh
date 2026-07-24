#!/bin/bash

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RESET='\033[0m'

echo -e "${YELLOW}======================================${RESET}"
echo -e "${YELLOW} Florence Backend Installation Script${RESET}"
echo -e "${YELLOW}======================================${RESET}"

if [ "$EUID" -ne 0 ]; then
  echo -e "${RED}sudo required to start the installation.${RESET}"
  exit 1
fi

echo 'updating system...'
apt update -y && apt upgrade -y

if [ $? -eq 0 ]; then
  echo -e "${GREEN}system updated successfully${RESET}"
else
  echo -e "${RED}something went wrong while updating the system${RESET}"
  exit 1
fi

PACKAGES="nginx pandoc python3-certbot-nginx python3.12 python3.12-pip python3.12-venv docker docker-compose"
echo -e "installing packages '${PACKAGES}'"
apt install -y $PACKAGES

if [ $? -eq 0 ]; then
  echo -e "${GREEN}packages installed successfully${RESET}"
else
  echo -e "${RED}something went wrong while installing packages${RESET}"
  exit 1
fi

read -rp "do you want to generate .env file now? (y/n): " env_generation
if [ "$env_generation" == "y" ] || [ "$env_generation" == "Y" ]; then
  touch .env

  read -rp "enter your GOOGLE_APPLICATION_CREDENTIALS path: " path
  echo "GOOGLE_APPLICATION_CREDENTIALS='$path'" >> .env

  read -rp "enter your COLLECT_API_KEY: " collect_api_key
  echo "COLLECT_API_KEY=$collect_api_key" >> .env

  read -rp "enter your FRED_API_KEY: " fred_api_key
  echo "FRED_API_KEY=$fred_api_key" >> .env

  read -rp "enter your EVDS_API_KEY: " evds_api_key
  echo "EVDS_API_KEY=$evds_api_key" >> .env

  read -rp "enter your OPENROUTER_API_KEY: " openrouter_api_key
  echo "OPENROUTER_API_KEY=$openrouter_api_key" >> .env

  read -rp "enter your CUSTOM_API_KEY: " custom_api_key
  echo "CUSTOM_API_KEY=$custom_api_key" >> .env

  echo "generating secure postgress password and secret key..."
  echo "POSTGRES_PASSWORD=$(openssl rand -base64 32)" >> .env
  echo "SECRET_KEY=$(openssl rand -base64 32)" >> .env

  # Ports
  echo "POSTGRES_HOST_PORT=5433" >> .env
  echo "REDIS_HOST_PORT=5434" >> .env
  echo "SEARXNG_HOST_PORT=5435" >> .env
  echo "API_HOST_PORT=8080" >> .env

  # Hosts
  echo "POSTGRES_HOST=localhost" >> .env
  echo "REDIS_HOST=localhost" >> .env

  # Embedding
  echo "EMBEDDING_API_KEY=ollama" >> .env
  echo "EMBEDDING_BASE_URL=http://127.0.0.1:11434/v1" >> .env
  echo "EMBEDDING_MODEL=mxbai-embed-large" >> .env

  # LLM
  echo "OPENROUTER_URL=https://openrouter.ai/api/v1" >> .env
  echo "CUSTOM_URL=http://localhost:7777/v1" >> .env
  echo "CUSTOM_MODEL=gemma" >> .env

  # News search
  echo "NEWS_SEARCH_URL=http://localhost:5435/search" >> .env
  echo "NEWS_SEARCH_USER_AGENT=\"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36\"" >> .env

  echo -e "${GREEN}generated .env file successfully.${RESET}"
else
  echo "generation passed"
fi

echo "running containers..."
docker compose up -d
if [ $? -eq 0 ]; then
  echo -e "${GREEN}containers installed successfully${RESET}"
else
  echo -e "${RED}something went wrong while installing containers${RESET}"
  exit 1
fi

echo "copying settings..."
docker cp searxng/settings.yml searxng:/etc/searxng/
if [ $? -eq 0 ]; then
  echo -e "${GREEN}settings copied${RESET}"
else
  echo -e "${RED}something went wrong while copying settings${RESET}"
  exit 1
fi

echo "restarting containers to apply searxng settings..."
docker compose restart
if [ $? -eq 0 ]; then
  echo -e "${GREEN}containers restarted successfully${RESET}"
else
  echo -e "${RED}something went wrong while restarting containers${RESET}"
  exit 1
fi

echo -e "${GREEN}Florence Backend installation is complete!${RESET}"