# app:
#  prod
#  cicd
#  dev
#    : 

PREFIX=dev

docker compose -f configs/docker-compose-$PREFIX.yml --env-file configs/.env $@

# add app