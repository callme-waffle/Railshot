package dev.railshot;

import java.util.Map;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
public class HealthController {
    public static String message() {
        return "ready";
    }
    @GetMapping("/health")
    public Map<String, String> health() {
        return Map.of("status", message());
    }
}
