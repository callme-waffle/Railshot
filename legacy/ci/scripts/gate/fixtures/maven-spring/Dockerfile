FROM maven:3.9.9-eclipse-temurin-21 AS build
WORKDIR /app
COPY . .
RUN MAVEN_CONFIG= sh ./mvnw -B -ntp package
FROM eclipse-temurin:21-jre
WORKDIR /app
COPY --from=build /app/target/fixture-1.0.0.jar /app/app.jar
USER 10001
CMD ["java", "-jar", "/app/app.jar"]
