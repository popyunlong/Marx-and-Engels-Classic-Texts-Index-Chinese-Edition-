# BASE_IMAGE must be a retained tag of the already installed monitor image.
# No browser/dependency download and no package-manager execution.
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
WORKDIR /app
RUN rm -rf /app/src /app/scripts
COPY src ./src
COPY scripts ./scripts
ENV TZ=Asia/Shanghai
CMD ["node", "src/daemon.js"]
