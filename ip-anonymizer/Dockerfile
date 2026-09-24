FROM golang:1.22-alpine AS build
WORKDIR /src
COPY go.mod .
COPY main.go .
RUN go build -o /ip-anonymizer .

FROM alpine:3.20
COPY --from=build /ip-anonymizer /ip-anonymizer
EXPOSE 8080
ENTRYPOINT ["/ip-anonymizer"]
