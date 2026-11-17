# syntax=docker/dockerfile:1
FROM eclipse-temurin:21.0.11_10-jdk-noble@sha256:a871f3e3caddad75608fd4531ed8bbca5cc42a27dc1da3ea3a2e554772b0ee15 AS build
WORKDIR /source
COPY --from=backend_source /gradle /source/gradle
COPY --from=backend_source /gradlew /build-tools/gradlew
COPY --from=backend_source /build.gradle.kts /settings.gradle.kts /source/
COPY --from=backend_source /src/main /source/src/main
RUN sed 's/\r$//' /build-tools/gradlew > /source/gradlew && \
    sh /source/gradlew --no-daemon bootJar --quiet && \
    cp /source/build/libs/backend-*.jar /backend.jar

FROM eclipse-temurin:21.0.11_10-jre-noble@sha256:b251c6e60516a48d6511a33b9afc2f8b1a427e53a598b16c1f29390c9bf916bd
ARG BACKEND_SHA
LABEL org.opencontainers.image.revision=$BACKEND_SHA
WORKDIR /opt/matchuri
COPY --from=build /backend.jar ./backend.jar
COPY application-perf.yaml ./config/application-perf.yaml
USER 10001:10001
ENTRYPOINT ["java", "-jar", "/opt/matchuri/backend.jar"]
