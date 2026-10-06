package io.devcontext.parser;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class RepositoryJavaAnalyzerTest {
    @TempDir Path root;
    void source(String module, String name, String content) throws Exception {
        Path file = root.resolve(module + "/src/main/java/demo/" + name + ".java");
        Files.createDirectories(file.getParent()); Files.writeString(file, "package demo;\r\n" + content);
    }
    @Test void crossModuleGraphUsesDeclarationsAndGenericOverrides() throws Exception {
        source("api", "Api", "public interface Api<T> { T run(T value); }");
        source("impl", "Service", """
            public class Service implements Api<String> {
                public Service() {}
                public String run(String value) { return helper(value); }
                public String helper(String value) { return value; }
                public String helper(int value) { return String.valueOf(value); }
            }
            """);
        source("web", "Controller", """
            public class Controller {
                Api<String> api;
                public String execute() { new Service(); return api.run("ok"); }
            }
            """);
        var result = new RepositoryJavaAnalyzer().analyze(root, "fixture");
        assertEquals(result.chunks().size(), result.symbols().size());
        assertTrue(result.edges().stream().anyMatch(e -> e.edgeType().equals("CALLS")
            && e.sourceSymbolKey().equals("M:demo.Controller#execute()") && e.targetSymbolKey().equals("M:demo.Api#run(java.lang.Object)")));
        assertTrue(result.edges().stream().anyMatch(e -> e.edgeType().equals("OVERRIDES")
            && e.sourceSymbolKey().equals("M:demo.Service#run(java.lang.String)") && e.targetSymbolKey().equals("M:demo.Api#run(java.lang.Object)")));
        assertTrue(result.edges().stream().anyMatch(e -> e.edgeType().equals("CONSTRUCTS") && e.targetSymbolKey().equals("C:demo.Service#<init>()")));
        assertTrue(result.edges().stream().anyMatch(e -> e.targetSymbolKey().equals("M:demo.Service#helper(java.lang.String)")));
        assertFalse(result.edges().stream().anyMatch(e -> e.targetSymbolKey().equals("M:demo.Service#helper(int)")));
        assertEquals(result.symbols().size(), new HashSet<>(result.symbols().stream().map(SymbolRecord::symbolKey).toList()).size());
    }
    @Test void missingDependenciesAreGapsWhileDuplicateIdentityIsFatal() throws Exception {
        source("one", "Broken", "public class Broken { Missing method(Missing x) { return x; } void ok() { missing.call(); } }");
        var result = new RepositoryJavaAnalyzer().analyze(root, "fixture");
        assertEquals(3, result.chunks().size());
        assertEquals(2, result.symbols().size());
        assertTrue(result.edges().isEmpty());
        assertFalse(((List<?>) result.diagnostics().get("gaps")).isEmpty());
        source("two", "Broken", "public class Broken {}");
        assertThrows(AnalysisContractException.class, () -> new RepositoryJavaAnalyzer().analyze(root, "fixture"));
    }
    @Test void staticPrivateAndAnonymousCallsAreNotFalseOverridesOrOuterCalls() throws Exception {
        source("one", "Parent", "public class Parent { public static void hidden() {} private void secret() {} }");
        source("two", "Child", """
            public class Child extends Parent {
                public static void hidden() {} private void secret() {}
                public void helper() {}
                public void execute() { Runnable r = new Runnable() { public void run() { helper(); } }; }
            }
            """);
        var result = new RepositoryJavaAnalyzer().analyze(root, "fixture");
        assertTrue(result.edges().stream().anyMatch(e -> e.edgeType().equals("EXTENDS")));
        assertFalse(result.edges().stream().anyMatch(e -> e.edgeType().equals("OVERRIDES")));
        assertFalse(result.edges().stream().anyMatch(e -> e.sourceSymbolKey().equals("M:demo.Child#execute()") && e.edgeType().equals("CALLS")));
    }
    @Test void sourceSolverRecordGetterResolutionUsesTheConfiguredResolver() throws Exception {
        source("api", "Value", "public record Value(String name) {}");
        source("web", "Consumer", "public class Consumer { public void use(Value value) { print(value.name()); } public void print(String text) {} }");
        var result = new RepositoryJavaAnalyzer().analyze(root, "fixture");
        assertTrue(result.edges().stream().anyMatch(e -> e.targetSymbolKey().equals("M:demo.Consumer#print(java.lang.String)")));
        assertFalse(result.edges().stream().anyMatch(e -> e.targetSymbolKey().contains("Value#name")));
        assertTrue(((Map<?, ?>) result.diagnostics().get("counts")).containsKey("internal_target_not_indexed"));
    }
    @Test void implicitPublicInterfaceMethodsOverrideAcrossPackages() throws Exception {
        Path api = root.resolve("api/src/main/java/api/Contract.java");
        Path impl = root.resolve("impl/src/main/java/impl/Service.java");
        Files.createDirectories(api.getParent()); Files.createDirectories(impl.getParent());
        Files.writeString(api, "package api; public interface Contract { boolean execute(String value); void stop(); }");
        Files.writeString(impl, "package impl; public class Service implements api.Contract { public boolean execute(String value) { return true; } public void stop() {} }");
        var result = new RepositoryJavaAnalyzer().analyze(root, "fixture");
        assertEquals(2, result.edges().stream().filter(e -> e.edgeType().equals("OVERRIDES")).count());
    }
}
