package dev.marcusson.lasso

import java.security.SecureRandom
import java.util.Random

class CredentialGenerator(private val random: Random = SecureRandom()) {
    fun ssid(): String = "Lasso-" + pick(SSID_CHARS, 6)

    fun psk(): String = pick(PSK_CHARS, 24)

    /** 128-bit session id as 32 lowercase hex chars. */
    fun sid(): String = ByteArray(16).also(random::nextBytes).joinToString("") { "%02x".format(it) }

    private fun pick(alphabet: String, n: Int) =
        String(CharArray(n) { alphabet[random.nextInt(alphabet.length)] })

    private companion object {
        const val SSID_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789"
        const val PSK_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    }
}
