package io.devcontext.parser;

import com.github.javaparser.*;
import com.github.javaparser.ast.*;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.type.ClassOrInterfaceType;
import com.github.javaparser.resolution.MethodUsage;
import com.github.javaparser.resolution.declarations.*;
import com.github.javaparser.resolution.types.ResolvedReferenceType;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.*;

import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.function.Supplier;
import java.util.stream.Collectors;

/** Repository-internal source relations. Resolution gaps are diagnostic, never fuzzy edges. */
public final class RepositoryJavaAnalyzer {
    private static final Set<String> EXCLUDED = Set.of("target", ".git", ".idea");
    public record Result(List<ChunkRecord> chunks, List<SymbolRecord> symbols,
                         List<SymbolEdgeRecord> edges, Map<String, Object> diagnostics) {}
    private final Map<String, SymbolRecord> symbols = new LinkedHashMap<>();
    private final Map<Node, String> nodeKeys = new IdentityHashMap<>();
    private final Set<SymbolEdgeRecord> edges = new LinkedHashSet<>();
    private final Map<String, Integer> counts = new TreeMap<>();
    private final List<Map<String, Object>> gaps = new ArrayList<>();
    private final Set<String> repositoryTypes = new HashSet<>();
    private String repository;

    public Result analyze(Path root, String repository) throws Exception {
        symbols.clear(); nodeKeys.clear(); edges.clear(); counts.clear(); gaps.clear(); repositoryTypes.clear();
        this.repository = repository;
        List<Path> files;
        try (var paths = Files.walk(root)) {
            files = paths.filter(Files::isRegularFile).filter(p -> p.toString().endsWith(".java"))
                    .filter(p -> p.toString().replace('\\', '/').contains("/src/main/java/"))
                    .filter(p -> { for (Path part : root.relativize(p)) if (EXCLUDED.contains(part.toString())) return false; return true; })
                    .sorted().toList();
        }
        Set<Path> roots = new TreeSet<>();
        for (Path file : files) {
            Path p = file.getParent();
            while (p != null && !p.toString().replace('\\', '/').endsWith("/src/main/java")) p = p.getParent();
            if (p != null) roots.add(p);
        }
        CombinedTypeSolver solver = new CombinedTypeSolver();
        ParserConfiguration sharedConfiguration = configuration();
        roots.forEach(p -> solver.add(new JavaParserTypeSolver(p, sharedConfiguration)));
        solver.add(new ReflectionTypeSolver());
        sharedConfiguration.setSymbolResolver(new JavaSymbolSolver(solver));
        JavaParser parser = new JavaParser(sharedConfiguration);
        JavaSourceParser chunkParser = new JavaSourceParser();
        List<ChunkRecord> chunks = new ArrayList<>();
        List<CompilationUnit> units = new ArrayList<>();
        for (Path file : files) {
            CompilationUnit unit;
            String source;
            try {
                source = Files.readString(file, StandardCharsets.UTF_8);
                var parsed = parser.parse(source);
                if (!parsed.isSuccessful() || parsed.getResult().isEmpty())
                    throw new IllegalArgumentException(parsed.getProblems().toString());
                unit = parsed.getResult().get();
                unit.setStorage(file);
            } catch (Exception error) { gap("parse_failed", root.relativize(file).toString(), 0, error); continue; }
            units.add(unit);
            for (TypeDeclaration<?> type : unit.findAll(TypeDeclaration.class))
                type.getFullyQualifiedName().ifPresent(repositoryTypes::add);
            List<ChunkRecord> fileChunks = chunkParser.chunks(root, file, repository, source, unit);
            List<Node> declarations = new ArrayList<>();
            declarations.addAll(unit.findAll(ClassOrInterfaceDeclaration.class));
            declarations.addAll(unit.findAll(MethodDeclaration.class));
            declarations.addAll(unit.findAll(ConstructorDeclaration.class));
            if (declarations.size() != fileChunks.size()) throw new AnalysisContractException("Chunk/declaration mismatch: " + file);
            for (int i = 0; i < declarations.size(); i++) {
                Node node = declarations.get(i);
                ChunkRecord chunk = fileChunks.get(i);
                int ref = chunks.size() + i;
                attempt(node, "symbol_unresolved", () -> register(node, chunk, ref));
            }
            chunks.addAll(fileChunks);
        }
        if (!files.isEmpty() && units.isEmpty()) throw new AnalysisContractException("All Java files failed to parse");
        for (CompilationUnit unit : units) {
            for (ClassOrInterfaceDeclaration type : unit.findAll(ClassOrInterfaceDeclaration.class)) {
                if (!nodeKeys.containsKey(type)) continue;
                for (ClassOrInterfaceType parent : type.getExtendedTypes())
                    attempt(parent, "relation_unresolved", () -> { relation(type, parent.resolve().asReferenceType().getQualifiedName(), "EXTENDS", parent); return null; });
                for (ClassOrInterfaceType parent : type.getImplementedTypes())
                    attempt(parent, "relation_unresolved", () -> { relation(type, parent.resolve().asReferenceType().getQualifiedName(), "IMPLEMENTS", parent); return null; });
            }
            for (MethodDeclaration method : unit.findAll(MethodDeclaration.class)) {
                if (nodeKeys.containsKey(method)) attempt(method, "override_unresolved", () -> { overrides(method); return null; });
            }
            for (MethodCallExpr call : unit.findAll(MethodCallExpr.class)) {
                Node owner = callableOwner(call);
                if (owner != null && nodeKeys.containsKey(owner)) attempt(call, "call_unresolved", () -> {
                    ResolvedMethodDeclaration target = call.resolve();
                    addEdge(nodeKeys.get(owner), callableKey(target, false), "CALLS", call, "SYMBOL_SOLVER_EXACT", target.declaringType().getQualifiedName());
                    return null;
                });
            }
            for (ObjectCreationExpr call : unit.findAll(ObjectCreationExpr.class)) {
                Node owner = callableOwner(call);
                if (owner != null && nodeKeys.containsKey(owner)) attempt(call, "construct_unresolved", () -> {
                    var target = call.resolve();
                    addEdge(nodeKeys.get(owner), callableKey(target, true), "CONSTRUCTS", call, "SYMBOL_SOLVER_EXACT", target.declaringType().getQualifiedName());
                    return null;
                });
            }
        }
        for (SymbolRecord symbol : symbols.values()) {
            if (symbol.ownerSymbolKey() != null && !symbols.containsKey(symbol.ownerSymbolKey()))
                throw new AnalysisContractException("Missing owner: " + symbol.symbolKey());
        }
        Map<String, Object> diagnostics = new LinkedHashMap<>();
        diagnostics.put("schema_version", 1); diagnostics.put("scanned_files", files.size());
        diagnostics.put("parsed_files", units.size()); diagnostics.put("source_roots", roots.stream().map(Path::toString).toList());
        diagnostics.put("code_chunks", chunks.size()); diagnostics.put("symbols", symbols.size());
        diagnostics.put("edges", edges.size()); diagnostics.put("counts", counts); diagnostics.put("gaps", gaps);
        return new Result(chunks, List.copyOf(symbols.values()), List.copyOf(edges), diagnostics);
    }

    private static ParserConfiguration configuration() {
        return new ParserConfiguration().setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21).setCharacterEncoding(StandardCharsets.UTF_8);
    }
    private Void register(Node node, ChunkRecord chunk, int ref) {
        String key, name, qualified, signature = null, owner = null, kind;
        if (node instanceof ClassOrInterfaceDeclaration type) {
            qualified = type.getFullyQualifiedName().orElseThrow(() -> new UnsupportedOperationException("Local type has no stable qualified name"));
            repositoryTypes.add(qualified);
            key = "T:" + qualified; name = type.getNameAsString(); kind = type.isInterface() ? "INTERFACE" : "CLASS";
            var parent = type.findAncestor(ClassOrInterfaceDeclaration.class);
            if (parent.isPresent()) {
                owner = nodeKeys.get(parent.get());
                if (owner == null) throw new UnsupportedOperationException("Owner not indexed");
            }
        } else {
            Node type = node.findAncestor(TypeDeclaration.class).orElseThrow();
            owner = nodeKeys.get(type);
            if (owner == null) throw new UnsupportedOperationException("Unsupported or unresolved owner");
            boolean constructor = node instanceof ConstructorDeclaration;
            ResolvedMethodLikeDeclaration declaration = constructor ? ((ConstructorDeclaration) node).resolve() : ((MethodDeclaration) node).resolve();
            key = callableKey(declaration, constructor);
            name = constructor ? declaration.declaringType().getName() : declaration.getName();
            signature = key.substring(key.indexOf('#') + 1);
            qualified = declaration.declaringType().getQualifiedName() + "#" + (constructor ? "<init>" : name);
            kind = constructor ? "CONSTRUCTOR" : "METHOD";
        }
        if (symbols.containsKey(key)) throw new AnalysisContractException("Duplicate symbol identity: " + key);
        symbols.put(key, new SymbolRecord(repository, key, kind, name, qualified, signature, owner, ref,
                chunk.filePath(), chunk.startLine(), chunk.endLine()));
        nodeKeys.put(node, key);
        return null;
    }
    static String callableKey(ResolvedMethodLikeDeclaration declaration, boolean constructor) {
        String parameters = declaration.formalParameterTypes().stream().map(t -> t.erasure().describe()).collect(Collectors.joining(","));
        return (constructor ? "C:" : "M:") + declaration.declaringType().getQualifiedName() + "#"
                + (constructor ? "<init>" : declaration.getName()) + "(" + parameters + ")";
    }
    private void relation(Node source, String targetType, String kind, Node site) {
        addEdge(nodeKeys.get(source), "T:" + targetType, kind, site, "SYMBOL_SOLVER_EXACT", targetType);
    }
    private void overrides(MethodDeclaration node) {
        ResolvedMethodDeclaration child = node.resolve();
        if (child.isStatic() || child.accessSpecifier() == AccessSpecifier.PRIVATE) return;
        MethodUsage childUsage = new MethodUsage(child);
        // Solver supplies ancestor generic substitutions and signature/return compatibility.
        // Unresolved ancestors are visited independently: a missing external superclass must
        // not erase a successfully resolved internal interface.
        Deque<ResolvedReferenceType> pending = new ArrayDeque<>(child.declaringType().getAncestors(true));
        Set<String> visited = new HashSet<>();
        while (!pending.isEmpty()) {
            ResolvedReferenceType ancestor = pending.remove();
            if (!visited.add(ancestor.describe())) continue;
            attempt(node, "override_unresolved", () -> {
                for (MethodUsage raw : ancestor.getDeclaredMethods()) {
                    ResolvedMethodDeclaration parent = raw.getDeclaration();
                    if (parent.isStatic() || parent.accessSpecifier() == AccessSpecifier.PRIVATE) continue;
                    if (parent.accessSpecifier() == AccessSpecifier.NONE && !parent.declaringType().isInterface()
                            && !child.getPackageName().equals(parent.getPackageName())) continue;
                    if (!parent.getName().equals(child.getName())) continue;
                    MethodUsage usage = raw;
                    for (int i = 0; i < usage.getNoParams(); i++)
                        usage = usage.replaceParamType(i, ancestor.typeParametersMap().replaceAll(usage.getParamType(i)));
                    usage = usage.replaceReturnType(ancestor.typeParametersMap().replaceAll(usage.returnType()));
                    boolean compatibleReturn = usage.returnType().equals(childUsage.returnType())
                            || (usage.returnType().isReferenceType() || usage.returnType().isTypeVariable())
                            && usage.returnType().isAssignableBy(childUsage.returnType());
                    if (childUsage.isSubSignature(usage) && compatibleReturn)
                        addEdge(nodeKeys.get(node), callableKey(parent, false), "OVERRIDES", node, "DERIVED_EXACT", parent.declaringType().getQualifiedName());
                }
                pending.addAll(ancestor.getDirectAncestors());
                return null;
            });
        }
    }
    private void addEdge(String source, String target, String kind, Node site, String resolution, String targetType) {
        if (!symbols.containsKey(target)) {
            increment(repositoryTypes.contains(targetType) ? "internal_target_not_indexed" : "resolved_external");
            return;
        }
        var begin = site.getBegin().orElseThrow(() -> new AnalysisContractException("Missing source position"));
        edges.add(new SymbolEdgeRecord(repository, source, target, kind, begin.line, begin.column, resolution));
        increment("resolved_internal");
    }
    private static Node callableOwner(Node site) {
        Node parent = site.getParentNode().orElse(null);
        while (parent != null) {
            if (parent instanceof MethodDeclaration || parent instanceof ConstructorDeclaration) return parent;
            if (parent instanceof TypeDeclaration<?> || parent instanceof ObjectCreationExpr creation && creation.getAnonymousClassBody().isPresent()) return null;
            parent = parent.getParentNode().orElse(null);
        }
        return null;
    }
    private <T> T attempt(Node node, String reason, Supplier<T> operation) {
        try { return operation.get(); }
        catch (AnalysisContractException fatal) { throw fatal; }
        catch (com.github.javaparser.resolution.UnsolvedSymbolException | UnsupportedOperationException | IllegalArgumentException error) {
            gap(reason, node.findCompilationUnit().flatMap(CompilationUnit::getStorage).map(s -> s.getPath().toString()).orElse(""),
                    node.getBegin().map(p -> p.line).orElse(0), error);
            return null;
        }
    }
    private void increment(String reason) { counts.merge(reason, 1, Integer::sum); }
    private void gap(String reason, String file, int line, Exception error) {
        increment(reason);
        gaps.add(Map.of("reason", reason, "file_path", file, "line", line,
                "message", String.valueOf(error.getMessage())));
    }
}
