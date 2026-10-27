package dev.marcusson.lasso

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.util.Random

class CredentialGeneratorTest {
    private val gen = CredentialGenerator(Random(42))

    @Test fun ssidFormat() = repeat(50) {
        assertTrue(gen.ssid().matches(Regex("Lasso-[a-z0-9]{6}")))
    }

    @Test fun pskFormat() = repeat(50) {
        assertTrue(gen.psk().matches(Regex("[A-Za-z0-9]{24}")))
    }

    @Test fun sidFormat() = repeat(50) {
        assertTrue(gen.sid().matches(Regex("[0-9a-f]{32}")))
    }

    @Test fun successiveValuesDiffer() {
        assertNotEquals(gen.psk(), gen.psk())
        assertNotEquals(gen.sid(), gen.sid())
    }

    @Test fun defaultConstructorUsesSecureRandom() {
        assertEquals(32, CredentialGenerator().sid().length)
    }
}
