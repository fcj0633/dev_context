package io.devcontext.parser;

public record SymbolRecord(
        String repository, String symbolKey, String symbolKind, String simpleName,
        String qualifiedName, String canonicalSignature, String ownerSymbolKey,
        int chunkRef, String filePath, int startLine, int endLine) {}
