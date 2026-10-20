FROM alpine:latest

# Docker supplies proxy build arguments without persisting them as image ENV.
# Forward host settings with: docker build --build-arg HTTP_PROXY \
#   --build-arg HTTPS_PROXY --build-arg NO_PROXY .
# Lowercase proxy arguments are also supported; HTTPS defaults to HTTP_PROXY.
RUN export http_proxy="${http_proxy:-$HTTP_PROXY}" \
		   https_proxy="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-$HTTP_PROXY}}}" \
		   no_proxy="${no_proxy:-$NO_PROXY}" \
	&& apk update && apk upgrade \
	&& apk add --no-cache xvfb py3-pip xterm build-base python3-dev openssl-dev

WORKDIR /app

COPY . .

RUN export http_proxy="${http_proxy:-$HTTP_PROXY}" \
		   https_proxy="${https_proxy:-${HTTPS_PROXY:-${http_proxy:-$HTTP_PROXY}}}" \
		   no_proxy="${no_proxy:-$NO_PROXY}" \
	&& pip install --break-system-packages -r requirements.txt

CMD [ "python3", "-m", "webx11.server", "--host", "0.0.0.0", "xterm" ]
