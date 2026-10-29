FROM --platform=$BUILDPLATFORM golang:1.24.8-alpine3.22 AS build

ARG MC_VERSION=RELEASE.2025-08-13T08-35-41Z

RUN mkdir -p /out && \
    GOBIN=/out go install -trimpath -ldflags="-s -w" \
    "github.com/minio/mc@${MC_VERSION}"

FROM alpine:3.22

RUN apk add --no-cache ca-certificates

COPY --from=build /out/mc /usr/local/bin/mc

ENTRYPOINT ["mc"]
