package dev.marcusson.lasso

import android.content.Context
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow

/** Paired laptop addresses (SharedPreferences string set) plus their display names. */
class Allowlist(context: Context) {
    private val prefs = context.getSharedPreferences("allowlist", Context.MODE_PRIVATE)
    private val _paired = MutableStateFlow(load())

    /** address -> laptop name */
    val paired: StateFlow<Map<String, String>> = _paired

    fun contains(address: String) = address in _paired.value

    fun nameOf(address: String) = _paired.value[address] ?: address

    fun add(address: String, name: String) {
        prefs.edit()
            .putStringSet(KEY, _paired.value.keys + address)
            .putString(NAME_PREFIX + address, name)
            .apply()
        _paired.value = load()
    }

    fun remove(address: String) {
        prefs.edit()
            .putStringSet(KEY, _paired.value.keys - address)
            .remove(NAME_PREFIX + address)
            .apply()
        _paired.value = load()
    }

    private fun load(): Map<String, String> =
        (prefs.getStringSet(KEY, null) ?: emptySet()).associateWith { prefs.getString(NAME_PREFIX + it, null) ?: it }

    private companion object {
        const val KEY = "addresses"
        const val NAME_PREFIX = "name_"
    }
}
