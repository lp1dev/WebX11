FROM alpine:latest

RUN apk update && apk upgrade

RUN apk add --no-cache xvfb py3-pip xterm build-base python3-dev openssl-dev

WORKDIR /app

COPY . .

RUN pip install --break-system-packages -r requirements.txt

RUN pip install --break-system-packages numpy mss

CMD [ "python3", "-m", "webx11.server", "--host", "0.0.0.0", "xterm" ]
