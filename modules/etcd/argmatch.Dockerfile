# etcd, built under argmatch and contexttrack, like prometheus/prombench-argmatch.Dockerfile (whose comments describe
# the ARGMATCH_* args). Build from the parent of the checkouts ($CT), as paramtrack's testbeds.py does:
#   docker build -f <this file> -t etcd-conftamer:v3.5.15 .
# The checkout (etcd-paramtrack) is upstream's v3.5.15, the version the testbed's kind node runs (kubeadm's static pod).
ARG BUILDER=prometheus-builder:go1.26.4  # (testbeds/prombench/upstream.py's BUILDER_TAG)
ARG STOCK=registry.k8s.io/etcd:3.5.15-0

FROM ${BUILDER} AS build
ARG ARGMATCH_FUNC=main.main
ARG ARGMATCH_MATCH=
ARG ARGMATCH_DEPTH=5
ARG ARGMATCH_MAXELEMS=64
ARG ARGMATCH_MAXFIELDS=-1
ARG ARGMATCH_MAXTIME=0
ARG ARGMATCH_MAXMEM=0
ARG ARGMATCH_MAXCALLS=3
ARG ARGMATCH_CALLINTERVAL=0
ARG ARGMATCH_MATCHFILE=
ARG ARGMATCH_STACKLOG=
ARG ARGMATCH_RECVONLY=
ARG ARGMATCH_EXACT=true
ARG ARGMATCH_IGNORECLOSURES=true
ARG ARGMATCH_IGNORELIBS=true
ARG ARGMATCH_IGNORETYPES=
ARG ARGMATCH_OUT=
ARG ARGMATCH_STATS=false
# Patch Go's net/http to log HTTP messages, and etcd's dependencies that serve and send its gRPC: grpc-go's server
# (etcd serves gRPC through net/http on a TLS client listener, grpcHandlerFunc, and through grpc-go's own server on a
# plain or gRPC-only one) and client (the gRPC gateway's and the server's own connections), and x/net's HTTP/2 server
# (http2.ConfigureServer on the TLS listener); see
# conftamer/contexttrack/README.md. Nothing is logged unless CONFTAMER_EVENTS is set.
COPY conftamer/contexttrack/go-inlibrary.patch conftamer/contexttrack/grpc-route.patch \
     conftamer/contexttrack/grpc-client.patch conftamer/contexttrack/x-net-http2-server.sh /patches/
RUN cd /usr/local/go && patch -p4 < /patches/go-inlibrary.patch
COPY golang.org-x-tools/gopls/conftamer/stacklog/ /golang.org-x-tools/gopls/conftamer/stacklog/
COPY dump_ancestors/argmatch/ /dump_ancestors/argmatch/
RUN cd /dump_ancestors/argmatch && go build -o argmatch .
COPY etcd-paramtrack/ /src/
# The server is its own Go module (server/, with its main package), replacing etcd's other modules by ../<dir>
RUN --mount=type=cache,target=/root/.cache/go-build \
    --mount=type=cache,target=/go/pkg/mod \
    cd /src/server && \
    go mod download google.golang.org/grpc golang.org/x/net && \
    grpc=$(go list -m -f '{{.Dir}}' google.golang.org/grpc) && \
    rm -rf /grpc-conftamer && cp -r "$grpc" /grpc-conftamer && chmod -R u+w /grpc-conftamer && \
    (cd /grpc-conftamer && patch -p1 < /patches/grpc-route.patch && patch -p1 < /patches/grpc-client.patch) && \
    go mod edit -replace=google.golang.org/grpc=/grpc-conftamer && \
    xnet=$(go list -m -f '{{.Dir}}' golang.org/x/net) && \
    rm -rf /xnet-conftamer && cp -r "$xnet" /xnet-conftamer && chmod -R u+w /xnet-conftamer && \
    sh /patches/x-net-http2-server.sh /xnet-conftamer && \
    go mod edit -replace=golang.org/x/net=/xnet-conftamer && \
    ARGMATCH_FUNC="${ARGMATCH_FUNC}" ARGMATCH_MATCH="${ARGMATCH_MATCH}" ARGMATCH_MATCHFILE="${ARGMATCH_MATCHFILE}" ARGMATCH_STACKLOG="${ARGMATCH_STACKLOG}" ARGMATCH_RECVONLY="${ARGMATCH_RECVONLY}" ARGMATCH_EXACT="${ARGMATCH_EXACT}" ARGMATCH_IGNORECLOSURES="${ARGMATCH_IGNORECLOSURES}" ARGMATCH_IGNORELIBS="${ARGMATCH_IGNORELIBS}" ARGMATCH_IGNORETYPES="${ARGMATCH_IGNORETYPES}" ARGMATCH_DEPTH="${ARGMATCH_DEPTH}" ARGMATCH_MAXELEMS="${ARGMATCH_MAXELEMS}" ARGMATCH_MAXFIELDS="${ARGMATCH_MAXFIELDS}" ARGMATCH_MAXTIME="${ARGMATCH_MAXTIME}" ARGMATCH_MAXMEM="${ARGMATCH_MAXMEM}" ARGMATCH_MAXCALLS="${ARGMATCH_MAXCALLS}" ARGMATCH_CALLINTERVAL="${ARGMATCH_CALLINTERVAL}" ARGMATCH_OUT="${ARGMATCH_OUT}" ARGMATCH_STATS="${ARGMATCH_STATS}" \
    GOFLAGS=-toolexec=/dump_ancestors/argmatch/argmatch CGO_ENABLED=0 GOTOOLCHAIN=local \
    go build -ldflags "-X go.etcd.io/etcd/api/v3/version.GitSHA=9a55333" -o /etcd .

# The stock image, with this binary in place of its own (kubeadm's manifest runs `etcd`)
FROM ${STOCK}
COPY --from=build /etcd /usr/local/bin/etcd
# Include up to 100 creating goroutines in tracebacks
ENV GODEBUG=tracebackancestors=100
