FROM --platform=$BUILDPLATFORM golang:1.24.8-alpine3.22 AS build

ARG TARGETARCH
ARG MINIO_VERSION=RELEASE.2025-10-15T17-29-55Z

RUN apk add --no-cache git

RUN git clone --depth 1 --branch "${MINIO_VERSION}" \
    https://github.com/minio/minio.git /src

WORKDIR /src

RUN mkdir -p /out && \
    GOOS=linux GOARCH="${TARGETARCH}" CGO_ENABLED=0 \
    go build -tags kqueue -trimpath -o /out/minio .

FROM alpine:3.22

RUN apk add --no-cache ca-certificates

COPY --from=build /out/minio /usr/local/bin/minio

VOLUME ["/data"]
EXPOSE 9000 9001
ENTRYPOINT ["minio"]
CMD ["server", "/data", "--console-address", ":9001"]
