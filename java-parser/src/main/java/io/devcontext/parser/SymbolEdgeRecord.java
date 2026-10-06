package io.devcontext.parser;

public record SymbolEdgeRecord(
        String repository, String sourceSymbolKey, String targetSymbolKey,
        String edgeType, int sourceLine, int sourceColumn, String resolutionKind) {}
