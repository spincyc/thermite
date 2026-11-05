/* The host build's mbedTLS: only what thermite_link uses. ESP-IDF builds use its own configuration. */
#define MBEDTLS_AES_C
#define MBEDTLS_CCM_C
#define MBEDTLS_CIPHER_C
