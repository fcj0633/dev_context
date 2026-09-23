package io.devcontext.parser;

import java.util.List;

public record ChunkRecord(
        String repository,
        String sourceType,
        String chunkType,
        String filePath,
        String module,
        String packageName,
        String className,
        String symbolName,
        String signature,
        List<String> annotations,
        String javadoc,
        String title,
        List<String> headingPath,
        String content,
        Integer startLine,
        Integer endLine,
        String contentHash
) {
}
