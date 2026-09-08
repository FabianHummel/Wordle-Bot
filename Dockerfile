FROM python:latest
LABEL Maintainer="m-ue-d"

WORKDIR /bot

COPY ./requirements.txt .
COPY ./SFNS.ttf .

RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["sh", "-c", "python generate_emojis.py && python bot.py"]
