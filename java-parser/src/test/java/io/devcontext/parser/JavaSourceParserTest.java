package io.devcontext.parser;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

class JavaSourceParserTest {
    @TempDir
    Path temporaryDirectory;

    @Test
    void parsesJava21InterfaceMethodAndConstructorWithCrlf() throws Exception {
        Path source = temporaryDirectory.resolve("order/src/main/java/demo/TicketService.java");
        Files.createDirectories(source.getParent());
        String java = "package demo;\r\n"
                + "public class TicketService implements Handler {\r\n"
                + "  public TicketService() {}\r\n"
                + "  /** Buy one ticket. */\r\n"
                + "  @Override\r\n"
                + "  public String purchaseTicket(String id) {\r\n"
                + "    return \"ticket:\" + id;\r\n"
                + "  }\r\n"
                + "}\r\n"
                + "interface Handler { String purchaseTicket(String id); }\r\n";
        Files.writeString(source, java, StandardCharsets.UTF_8);

        List<ChunkRecord> records = new JavaSourceParser().parse(temporaryDirectory, source, "test");

        assertEquals(2, records.stream().filter(record -> record.chunkType().equals("METHOD")).count());
        assertEquals(1, records.stream().filter(record -> record.chunkType().equals("CONSTRUCTOR")).count());
        ChunkRecord method = records.stream()
                .filter(record -> record.chunkType().equals("METHOD"))
                .filter(record -> record.className().equals("TicketService"))
                .findFirst().orElseThrow();
        assertEquals(5, method.startLine());
        assertEquals(8, method.endLine());
        assertTrue(method.content().contains("return \"ticket:\" + id;\r\n"));
        assertTrue(method.annotations().contains("@Override"));
        assertEquals("order", method.module());
    }
}
