```bash
python -m app.cli --cli-env-file .cli.env console --remote

git pull
docker compose up -d --build

docker compose logs -f

docker compose exec -T db psql -U twitch -d twitch_eventsub -c "select count(*) from service_interests"