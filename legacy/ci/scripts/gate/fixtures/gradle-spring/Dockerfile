FROM gradle:8.14.3-jdk21 AS build
WORKDIR /app
COPY . .
RUN sh ./gradlew --no-daemon --console=plain --dependency-verification=strict bootJar
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/build/libs/fixture-1.0.0.jar /app/app.jar
USER 10001
CMD ["java", "-jar", "/app/app.jar"]
