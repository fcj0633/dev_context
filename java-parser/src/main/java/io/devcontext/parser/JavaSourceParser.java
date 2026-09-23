package io.devcontext.parser;

import com.github.javaparser.JavaParser;
import com.github.javaparser.ParserConfiguration;
import com.github.javaparser.ParseResult;
import com.github.javaparser.Position;
import com.github.javaparser.Range;
import com.github.javaparser.ast.CompilationUnit;
import com.github.javaparser.ast.Node;
import com.github.javaparser.ast.body.ClassOrInterfaceDeclaration;
import com.github.javaparser.ast.body.ConstructorDeclaration;
import com.github.javaparser.ast.body.FieldDeclaration;
import com.github.javaparser.ast.body.MethodDeclaration;
import com.github.javaparser.ast.body.TypeDeclaration;
import com.github.javaparser.ast.nodeTypes.NodeWithAnnotations;
import com.github.javaparser.ast.nodeTypes.NodeWithJavadoc;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Optional;
import java.util.stream.Collectors;

public final class JavaSourceParser {
    private final JavaParser parser;

    public JavaSourceParser() {
        ParserConfiguration configuration = new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)
                .setCharacterEncoding(StandardCharsets.UTF_8);
        this.parser = new JavaParser(configuration);
    }

    public List<ChunkRecord> parse(Path root, Path file, String repository) throws IOException {
        String source = java.nio.file.Files.readString(file, StandardCharsets.UTF_8);
        ParseResult<CompilationUnit> result = parser.parse(source);
        CompilationUnit unit = result.getResult().orElseThrow(
                () -> new IllegalArgumentException("No compilation unit: " + result.getProblems())
        );
        if (!result.isSuccessful()) {
            throw new IllegalArgumentException("Parse problems: " + result.getProblems());
        }

        String relativePath = root.relativize(file).toString().replace('\\', '/');
        String module = detectModule(root.relativize(file));
        String packageName = unit.getPackageDeclaration()
                .map(declaration -> declaration.getNameAsString())
                .orElse(null);
        List<ChunkRecord> records = new ArrayList<>();

        for (ClassOrInterfaceDeclaration type : unit.findAll(ClassOrInterfaceDeclaration.class)) {
            records.add(typeRecord(
                    repository, relativePath, module, packageName, type, source
            ));
        }
        for (MethodDeclaration method : unit.findAll(MethodDeclaration.class)) {
            records.add(callableRecord(
                    repository, relativePath, module, packageName, method,
                    "METHOD", method.getNameAsString(), method.getDeclarationAsString(true, true, true), source
            ));
        }
        for (ConstructorDeclaration constructor : unit.findAll(ConstructorDeclaration.class)) {
            records.add(callableRecord(
                    repository, relativePath, module, packageName, constructor,
                    "CONSTRUCTOR", constructor.getNameAsString(), constructor.getDeclarationAsString(true, true, true), source
            ));
        }
        return records;
    }

    private ChunkRecord typeRecord(
            String repository,
            String relativePath,
            String module,
            String packageName,
            ClassOrInterfaceDeclaration type,
            String source
    ) {
        Range range = type.getRange().orElseThrow();
        String declaration = typeDeclaration(type);
        StringBuilder summary = new StringBuilder();
        javadoc(type).ifPresent(value -> summary.append(value).append("\n"));
        annotations(type).forEach(value -> summary.append(value).append("\n"));
        summary.append(declaration).append(" {\n");
        for (FieldDeclaration field : type.getFields()) {
            summary.append("  ").append(field).append("\n");
        }
        for (ConstructorDeclaration constructor : type.getConstructors()) {
            summary.append("  ").append(constructor.getDeclarationAsString(true, true, true)).append(";\n");
        }
        for (MethodDeclaration method : type.getMethods()) {
            summary.append("  ").append(method.getDeclarationAsString(true, true, true)).append(";\n");
        }
        summary.append('}');
        String content = summary.toString();
        return new ChunkRecord(
                repository,
                "CODE",
                type.isInterface() ? "INTERFACE" : "CLASS",
                relativePath,
                module,
                packageName,
                type.getNameAsString(),
                type.getNameAsString(),
                declaration,
                annotations(type),
                javadoc(type).orElse(null),
                null,
                List.of(),
                content,
                range.begin.line,
                range.end.line,
                sha256(content)
        );
    }

    private ChunkRecord callableRecord(
            String repository,
            String relativePath,
            String module,
            String packageName,
            Node callable,
            String chunkType,
            String symbolName,
            String signature,
            String source
    ) {
        Range range = callable.getRange().orElseThrow();
        String content = slice(source, range);
        String className = callable.findAncestor(TypeDeclaration.class)
                .map(type -> type.getNameAsString())
                .orElse(null);
        return new ChunkRecord(
                repository,
                "CODE",
                chunkType,
                relativePath,
                module,
                packageName,
                className,
                symbolName,
                signature,
                annotations(callable),
                javadoc(callable).orElse(null),
                null,
                List.of(),
                content,
                range.begin.line,
                range.end.line,
                sha256(content)
        );
    }

    private static List<String> annotations(Node node) {
        if (node instanceof NodeWithAnnotations<?> annotated) {
            return annotated.getAnnotations().stream().map(Node::toString).toList();
        }
        return List.of();
    }

    private static Optional<String> javadoc(Node node) {
        if (node instanceof NodeWithJavadoc<?> documented) {
            return documented.getJavadocComment().map(comment -> comment.getContent().strip());
        }
        return Optional.empty();
    }

    static String slice(String source, Range range) {
        int start = offset(source, range.begin);
        int end = Math.min(source.length(), offset(source, range.end) + 1);
        return source.substring(start, end);
    }

    private static int offset(String source, Position position) {
        int line = 1;
        int index = 0;
        while (line < position.line && index < source.length()) {
            char current = source.charAt(index++);
            if (current == '\r') {
                if (index < source.length() && source.charAt(index) == '\n') {
                    index++;
                }
                line++;
            } else if (current == '\n') {
                line++;
            }
        }
        return Math.min(source.length(), index + position.column - 1);
    }

    private static String detectModule(Path relativePath) {
        for (int index = 0; index < relativePath.getNameCount(); index++) {
            if ("src".equals(relativePath.getName(index).toString()) && index > 0) {
                return relativePath.getName(index - 1).toString();
            }
        }
        return relativePath.getNameCount() > 1 ? relativePath.getName(0).toString() : null;
    }

    private static String typeDeclaration(ClassOrInterfaceDeclaration type) {
        StringBuilder declaration = new StringBuilder();
        type.getModifiers().forEach(modifier -> declaration
                .append(modifier.getKeyword().asString()).append(' '));
        declaration.append(type.isInterface() ? "interface " : "class ")
                .append(type.getNameAsString());
        if (!type.getTypeParameters().isEmpty()) {
            declaration.append(type.getTypeParameters().stream()
                    .map(Node::toString)
                    .collect(Collectors.joining(", ", "<", ">")));
        }
        if (!type.getExtendedTypes().isEmpty()) {
            declaration.append(" extends ").append(type.getExtendedTypes().stream()
                    .map(Node::toString).collect(Collectors.joining(", ")));
        }
        if (!type.getImplementedTypes().isEmpty()) {
            declaration.append(" implements ").append(type.getImplementedTypes().stream()
                    .map(Node::toString).collect(Collectors.joining(", ")));
        }
        if (!type.getPermittedTypes().isEmpty()) {
            declaration.append(" permits ").append(type.getPermittedTypes().stream()
                    .map(Node::toString).collect(Collectors.joining(", ")));
        }
        return declaration.toString();
    }

    private static String sha256(String value) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            return HexFormat.of().formatHex(digest.digest(value.getBytes(StandardCharsets.UTF_8)));
        } catch (NoSuchAlgorithmException exception) {
            throw new IllegalStateException(exception);
        }
    }
}
