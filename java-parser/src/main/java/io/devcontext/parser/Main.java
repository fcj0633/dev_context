package io.devcontext.parser;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;

import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Set;
import java.util.stream.StreamSupport;

public final class Main {
    private static final Set<String> EXCLUDED = Set.of("target", ".git", ".idea");

    private Main() {
    }

    public static void main(String[] args) throws Exception {
        Arguments arguments = Arguments.parse(args);
        Path root = arguments.codeRoot().toAbsolutePath().normalize();
        if (!Files.isDirectory(root)) {
            throw new IllegalArgumentException("Code root does not exist: " + root);
        }
        Path output = arguments.output().toAbsolutePath().normalize();
        Files.createDirectories(output.getParent());

        ObjectMapper mapper = new ObjectMapper();
        mapper.setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE);
        JavaSourceParser parser = new JavaSourceParser();
        List<Path> files = findJavaFiles(root);
        int parsed = 0;
        int failed = 0;
        int chunks = 0;

        try (BufferedWriter writer = Files.newBufferedWriter(output, StandardCharsets.UTF_8)) {
            for (Path file : files) {
                try {
                    List<ChunkRecord> records = parser.parse(root, file, arguments.repository());
                    for (ChunkRecord record : records) {
                        writer.write(mapper.writeValueAsString(record));
                        writer.newLine();
                        chunks++;
                    }
                    parsed++;
                } catch (Exception exception) {
                    failed++;
                    System.err.printf("PARSE_ERROR file=%s message=%s%n", root.relativize(file), exception.getMessage());
                }
            }
        }

        System.out.printf(
                "JAVA_PARSE_SUMMARY scanned=%d parsed=%d failed=%d chunks=%d output=%s%n",
                files.size(), parsed, failed, chunks, output
        );
        if (!files.isEmpty() && parsed == 0) {
            throw new IllegalStateException("All Java files failed to parse");
        }
    }

    private static List<Path> findJavaFiles(Path root) throws IOException {
        List<Path> files = new ArrayList<>();
        try (var paths = Files.walk(root)) {
            paths.filter(Files::isRegularFile)
                    .filter(path -> path.toString().endsWith(".java"))
                    .filter(path -> path.toString().replace('\\', '/').contains("/src/main/java/"))
                    .filter(path -> StreamSupport.stream(root.relativize(path).spliterator(), false)
                            .noneMatch(part -> EXCLUDED.contains(part.toString())))
                    .forEach(files::add);
        }
        files.sort(Comparator.naturalOrder());
        return files;
    }

    private record Arguments(Path codeRoot, Path output, String repository) {
        private static Arguments parse(String[] args) {
            Path codeRoot = null;
            Path output = null;
            String repository = "my12306";
            for (int index = 0; index < args.length; index++) {
                switch (args[index]) {
                    case "--code-root" -> codeRoot = Path.of(requireValue(args, ++index, "--code-root"));
                    case "--output" -> output = Path.of(requireValue(args, ++index, "--output"));
                    case "--repository" -> repository = requireValue(args, ++index, "--repository");
                    default -> throw new IllegalArgumentException("Unknown argument: " + args[index]);
                }
            }
            if (codeRoot == null || output == null) {
                throw new IllegalArgumentException("Required: --code-root <path> --output <path>");
            }
            return new Arguments(codeRoot, output, repository);
        }

        private static String requireValue(String[] args, int index, String option) {
            if (index >= args.length) {
                throw new IllegalArgumentException("Missing value for " + option);
            }
            return args[index];
        }
    }
}
